import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChallengeProcess } from './ChallengeProcess'

/*
  ══════════════════════════════════════════════════════════════
  题目会话里的「解题过程」
  ══════════════════════════════════════════════════════════════

  ## 为什么测它

  用户原话："对话没输出内容啊" —— 点看板「查看」进来，题目条写着
  「解题中」，对话区却是空的。过程本来在活动流里，现在搬到了这里。

  三条判据都不会报错，只会让人以为坏了：

  1. 没有 scanId（还没解过）→ 要**说清楚**，不能安静地什么都不画；
  2. 有事件 → 工具命令要真的显示出来（那是用户唯一能看懂的东西）；
  3. 读不到 → 出声，而不是显示"0 条"假装正常。

  ## ⚠️ 三个必须自己处理的坑（与 ErrorState.test.tsx 记录的一致）

  1. 必须手动 `cleanup`（本项目没开 vitest globals）；
  2. 不能用 `toHaveTextContent`（没装 jest-dom）；
  3. 定时器要清 —— 组件在 running 时会开 3 秒轮询。
*/
afterEach(cleanup)

const get = vi.fn()

vi.mock('@/lib/api', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
  errorMessage: (err: unknown) => (err instanceof Error ? err.message : String(err)),
}))

function event(over: Record<string, unknown> = {}) {
  return {
    id: 'evt_1',
    scanId: 'scan_1',
    agentId: 'agent_1',
    type: 'tool_call',
    summary: '执行 nmap',
    detail: { kind: 'tool_call', tool: 'nmap', command: 'nmap -sV 10.0.0.1', exitCode: 0 },
    at: '2026-11-08T10:00:00Z',
    ...over,
  }
}

beforeEach(() => {
  get.mockReset()
})

describe('ChallengeProcess', () => {
  it('还没有 scanId 时说清楚"还没开始解题"，不安静地空着', () => {
    render(<ChallengeProcess scanId={null} running={false} />)

    expect(screen.getByText(/还没有开始解题/)).not.toBeNull()
    expect(get).not.toHaveBeenCalled()
  })

  it('★ 工具调用显示成命令本身（用户唯一看得懂的东西）', async () => {
    get.mockResolvedValue({ events: [event()], hasMore: false })

    render(<ChallengeProcess scanId="scan_1" running />)

    await waitFor(() => expect(screen.getByText('nmap -sV 10.0.0.1')).not.toBeNull())
    expect(screen.getByText('1 条')).not.toBeNull()
    expect(screen.getByText('进行中')).not.toBeNull()
  })

  it('推理事件用摘要显示（它没有命令）', async () => {
    get.mockResolvedValue({
      events: [event({ id: 'e2', type: 'reasoning', detail: null, summary: '这一步先看文件头' })],
      hasMore: false,
    })

    render(<ChallengeProcess scanId="scan_1" running={false} />)

    await waitFor(() => expect(screen.getByText('这一步先看文件头')).not.toBeNull())
    expect(screen.queryByText('进行中')).toBeNull()
  })

  it('★ 不在跑时标明这是**上一次**运行的记录（别把旧事件当成现在）', async () => {
    get.mockResolvedValue({ events: [event()], hasMore: false })

    render(<ChallengeProcess scanId="scan_1" running={false} />)

    await waitFor(() => expect(screen.getByText('上一次运行的过程')).not.toBeNull())
    expect(screen.getByText(/这道题当前没有在跑/)).not.toBeNull()
    expect(screen.queryByText('进行中')).toBeNull()
  })

  it('读不到事件要出声，而不是显示 0 条装作正常', async () => {
    get.mockRejectedValue(new Error('502 Bad Gateway'))

    render(<ChallengeProcess scanId="scan_1" running={false} />)

    await waitFor(() => expect(screen.getByText(/读不到过程事件/)).not.toBeNull())
  })

  it('★ 扫描已被清理时说的是"过程不可读"，不是红字报警', async () => {
    // 后端的 404 文案。应急响应页的历史任务经常指向被清理掉的扫描 ——
    // 那是正常的保留期清理，不是故障。
    get.mockRejectedValue(new Error('扫描不存在，可能已被删除。'))

    render(<ChallengeProcess scanId="scan_1" running={false} />)

    await waitFor(() =>
      expect(screen.getByText(/扫描记录已被清理，过程不再可读/)).not.toBeNull(),
    )
    expect(screen.queryByText(/读不到过程事件/)).toBeNull()
  })

  it('跑着的题没有事件时说"刚开跑"（而不是"这一轮没有记录"）', async () => {
    get.mockResolvedValue({ events: [], hasMore: false })

    render(<ChallengeProcess scanId="scan_1" running />)

    await waitFor(() => expect(screen.getByText('刚开跑，还没有产生动作。')).not.toBeNull())
  })
})
