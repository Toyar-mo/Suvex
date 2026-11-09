import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ActivityEvent, AgentPhase } from '@/data/overview'
import { usePrefersReducedMotion } from '@/hooks/usePrefersReducedMotion'

import ActivityFlowStage from './ActivityFlowStage'
import { buildStageModel } from './stage-model'

/*
  ══════════════════════════════════════════════════════════════
  ActivityFlowStage 的测试
  ══════════════════════════════════════════════════════════════

  canvas 里的动画本身在 jsdom 里跑不起来（没有 2d 上下文），也不该用
  单元测试去断言"像素动了没有"—— 那是截图验收的事。

  这里钉的是**动效背后的数据语义**：
    · 各阶段计数与智能体签只取最近 12 条事件；
    · 每个智能体的签子挂在它最近一条事件所处的阶段；
    · 减少动效时不挂载 canvas，但标签与签子一个不少。
*/

vi.mock('@/hooks/usePrefersReducedMotion', () => ({
  usePrefersReducedMotion: vi.fn(() => false),
}))

const mockedReducedMotion = vi.mocked(usePrefersReducedMotion)

function makeEvent(id: string, phase: AgentPhase, at: string, agentId = 'agent-01'): ActivityEvent {
  return {
    id,
    scanId: 'scan_a',
    agentId,
    agentPhase: phase,
    type: 'tool_call',
    summary: `事件 ${id}`,
    subject: null,
    at,
  }
}

beforeEach(() => {
  mockedReducedMotion.mockReturnValue(false)
  // jsdom 没有 2D 上下文；返回 null 让组件安静地退到 DOM 层。
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})

afterEach(cleanup)

describe('buildStageModel', () => {
  it('统计最近窗口内的阶段数量、智能体归属与最新阶段', () => {
    const events = [
      makeEvent('e1', 'verify', '2026-11-09T10:03:00.000Z', 'agent-02'),
      makeEvent('e2', 'scan', '2026-11-09T10:02:00.000Z', 'agent-01'),
      makeEvent('e3', 'recon', '2026-11-09T10:01:00.000Z', 'agent-01'),
    ]

    const model = buildStageModel(events)

    expect(model.total).toBe(3)
    expect(model.counts.recon).toBe(1)
    expect(model.counts.scan).toBe(1)
    expect(model.counts.verify).toBe(1)
    expect(model.latestPhase).toBe('verify')
    expect(model.marksByPhase.scan.map((mark) => mark.agentId)).toEqual(['agent-01'])
    expect(model.marksByPhase.verify.map((mark) => mark.agentId)).toEqual(['agent-02'])
    // 同一智能体的两条事件只算一枚签，但计数是 2
    expect(model.marksByPhase.scan[0].count).toBe(2)
  })

  it('只取最近 limit 条事件，避免"总数"口径混进来', () => {
    const events = Array.from({ length: 16 }, (_, index) =>
      makeEvent(
        `e${index}`,
        'scan',
        `2026-11-09T10:${String(59 - index).padStart(2, '0')}:00.000Z`,
      ),
    )

    const model = buildStageModel(events, 12)

    expect(model.total).toBe(12)
    expect(model.counts.scan).toBe(12)
  })
})

describe('ActivityFlowStage', () => {
  it('画出五个阶段、计数与每个智能体的签子', () => {
    const events = [
      makeEvent('e1', 'verify', '2026-11-09T10:03:00.000Z', 'agent-02'),
      makeEvent('e2', 'scan', '2026-11-09T10:02:00.000Z', 'agent-01'),
    ]

    render(<ActivityFlowStage events={events} latestBatchIds={new Set()} flowing />)

    expect(screen.getByLabelText(/^扫描：最近 1 条事件，1 个智能体/)).toBeTruthy()
    expect(screen.getByLabelText(/^验证：最近 1 条事件，1 个智能体/)).toBeTruthy()
    expect(screen.getByLabelText(/^信息收集：最近 0 条事件，0 个智能体/)).toBeTruthy()
    expect(screen.getByText('agent-01')).toBeTruthy()
    expect(screen.getByText('agent-02')).toBeTruthy()
  })

  it('动画开启时挂载 canvas；减少动效时不挂载，但信息仍在', () => {
    const events = [makeEvent('e1', 'scan', '2026-11-09T10:02:00.000Z')]

    const first = render(<ActivityFlowStage events={events} latestBatchIds={new Set()} flowing />)
    expect(first.container.querySelector('canvas')).not.toBeNull()
    first.unmount()

    mockedReducedMotion.mockReturnValue(true)
    const second = render(<ActivityFlowStage events={events} latestBatchIds={new Set()} flowing />)
    expect(second.container.querySelector('canvas')).toBeNull()
    expect(screen.getByLabelText(/^扫描：最近 1 条事件，1 个智能体/)).toBeTruthy()
    expect(screen.getByText('agent-01')).toBeTruthy()
  })
})
