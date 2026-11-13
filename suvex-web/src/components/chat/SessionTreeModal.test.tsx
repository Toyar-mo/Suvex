import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChatSessionTreeNode } from '@/data/chat'
import { SessionTreeModal } from './SessionTreeModal'

afterEach(cleanup)

const nodes: ChatSessionTreeNode[] = [
  {
    id: 'root',
    title: '根会话',
    forkedFromSessionId: null,
    forkedFromSeq: null,
    branchSummary: null,
    messageCount: 12,
    createdAt: '2026-11-13T00:00:00Z',
    current: false,
  },
  {
    id: 'child',
    title: '换一条路',
    forkedFromSessionId: 'root',
    forkedFromSeq: 7,
    branchSummary: '另一个分支试过 X',
    messageCount: 3,
    createdAt: '2026-11-13T01:00:00Z',
    current: true,
  },
]

describe('SessionTreeModal', () => {
  it('renders each session node, fork point and current marker', () => {
    render(
      <SessionTreeModal
        open
        onClose={vi.fn()}
        nodes={nodes}
        onSelect={vi.fn()}
      />,
    )
    expect(screen.getAllByText('根会话').length).toBeGreaterThan(0)
    expect(screen.getAllByText('换一条路').length).toBeGreaterThan(0)
    expect(screen.getByText(/第 7 条消息/)).toBeTruthy()
    expect(screen.getByText(/含分支摘要/)).toBeTruthy()
    expect(screen.getByRole('button', { name: /换一条路/ }).getAttribute('aria-current')).toBe('page')
  })

  it('selects a node by id', () => {
    const onSelect = vi.fn()
    render(
      <SessionTreeModal open onClose={vi.fn()} nodes={nodes} onSelect={onSelect} />,
    )
    fireEvent.click(screen.getByRole('button', { name: /根会话/ }))
    expect(onSelect).toHaveBeenCalledWith('root')
  })

  it('separates loading and error from an empty tree', () => {
    const { rerender } = render(
      <SessionTreeModal open onClose={vi.fn()} nodes={[]} loading onSelect={vi.fn()} />,
    )
    expect(screen.getByText(/正在取分支树/)).toBeTruthy()
    rerender(
      <SessionTreeModal open onClose={vi.fn()} nodes={[]} error="网络错误" onSelect={vi.fn()} />,
    )
    expect(screen.getByText(/网络错误/)).toBeTruthy()
  })
})
